# EVO memory design and lifecycle

**Authoritative store:** one SQLite file, `data/evo.db` (WAL mode). Schema:
[`evo/memory/schema.sql`](../evo/memory/schema.sql). Everything else (the Excel register,
the Markdown cycle reports) is a generated view and is never read back as truth.

**Why SQLite and no vector database.** EVO's memory is structured: configs, scores,
statuses, versions and approvals that the agent filters and compares exactly
("every run of this config", "the current version of this fact"). Those are SQL
queries, not similarity searches. The only narrative material is a handful of tuning
notes, which SQLite FTS5 (BM25) indexes well enough and keeps inspectable.

## Memory layers

| Layer | Tables | Bounded how |
|---|---|---|
| Working state | `cycles.checkpoint_json` (step, ingest report, observations, new findings) | Last 6 observations go to the planner; checkpoint cleared when the cycle closes |
| Long-term | `project_facts`, `runs`, `run_metrics`, `diagnoses`, `hypotheses`, `lessons`, `proposals`, `approvals` | Brief includes open/last-5 closed hypotheses, all current lessons, last 8 failures, last 6 decisions |
| Episodic | `decisions` (every planner step), `cycles.summary_json` | Append-only |
| Source knowledge | `knowledge_docs` + `knowledge_fts` | Top-4 passages per search |
| Audit | `action_log`, `side_effects` | Append-only; side_effects keyed by idempotency key |
| Replay cache | `llm_cache` | Keyed by prompt hash |

## Lifecycle

| What | Saved when, by whom | Why | Retrieved when | Who can access | Corrected / deleted how |
|---|---|---|---|---|---|
| Project facts (goal, metric, target, budget, search space, deadline) | Onboarding after user approval; later only through an approved `fact_change` proposal | Defines what "good" and "allowed" mean | Every cycle (brief) and every gate check | That project's scope only | New version becomes `current`, old one `superseded` (`supersedes_id` link). Never deleted |
| Runs + file hashes | Ingest (user runs) or runner (agent runs) | Evidence; duplicate detection | Analysis, similar-config lookup | Project scope | A changed result file of a finished run becomes a *proposed* metric version that needs approval |
| Run metrics (eval curve) | Ingest; corrections via `evo correct-metric` | The numbers every decision rests on | Analysis | Project scope | Versioned like facts; approval promotes the new version, old stays as `superseded` |
| Diagnoses | Analyser / ingest (deterministic) | Findings the planner reacts to | Brief (new this cycle + past failures) | Project scope | Superseded when the metric version they were computed from is replaced |
| Hypotheses | Planner (`open_hypothesis`); resolution only after user approval | Track what is being tested and what happened | Every memory-on cycle | Project scope | Status changes `open -> supported / refuted / inconclusive`; statement and evidence kept |
| Lessons | Automatically when every seed of a config collapsed/diverged (`auto:evidence`), or when the user approves a hypothesis resolution | Steer later proposals; the gate blocks `avoid` regions in memory-on mode | Brief and policy gate | Project scope | `evo lesson-reject` marks it `rejected` (kept for audit) |
| Proposals + approvals | Planner creates; user decides via CLI | Approval boundary; separates agent suggestions from user decisions | Status screen, brief (pending ones) | Project scope | Status only moves forward; approvals are append-only |
| Decisions | Every planner step | Auditable reasoning (action, rationale, evidence, gate result, model, prompt hash) | Brief (recent), reports, register | Project scope | Append-only |
| Action log / side effects | Every tool call | Audit trail, retries without duplicates | `evo log`, crash reconciliation | Operator | Append-only |
| Knowledge notes | `evo init` / start of every cycle (re-indexed if the file hash changed) | External evidence for diagnoses | `search_knowledge` | All projects, read-only | Edit the Markdown file; version + hash recorded |

## Isolation

The agent never writes SQL. It receives a `ProjectScope` bound to one `project_id`;
every repository method filters on it, and lookups by name or ID that hit another
project's row raise `IsolationError` (logged as a denied read). Knowledge notes are the
only shared data and are read-only.

## Excel register

`projects/<id>/reports/register.xlsx` is rebuilt from the DB on each export. Columns
whose header starts with **User** belong to the user: they are read back from the
previous file by the row's stable key (`run:<name>`, `hyp:H1`, ...) and written again
unchanged, so an export never overwrites user notes. Binding decisions are recorded with
`evo approve` / `evo reject`, not by editing the sheet.

## Untrusted content

Logs, configs and notes are data. They are scanned for instruction-like text (flagged as
an `untrusted_instruction` diagnosis) and reach the LLM only inside fenced
`<<<UNTRUSTED_DATA ...>>>` blocks. Permissions, budget, the search space and the approval
rule come only from approved facts and code (`evo/policy.py`).
