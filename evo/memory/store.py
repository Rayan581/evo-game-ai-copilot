"""SQLite store and the project-scoped repository the agent uses.

The agent never writes SQL. Everything goes through `ProjectScope`, which is
bound to one project_id and filters every query on it. Reads and writes are
recorded on a `MemoryTracker` so the action log can show exactly which memory
each tool call touched.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from evo.util import jdumps, jloads, now_iso, stable_hash

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = "1"


class IsolationError(PermissionError):
    """Raised when a scoped repository is asked for another project's data."""


class NotFound(LookupError):
    pass


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)  # autocommit; explicit tx below
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, so multi-row updates are atomic."""
    if conn.in_transaction:  # nested: join the outer transaction
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


@dataclass
class MemoryTracker:
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)

    def read(self, what: str) -> None:
        self.reads.append(what)

    def write(self, what: str) -> None:
        self.writes.append(what)

    def drain(self) -> tuple[list[str], list[str]]:
        r, w = self.reads, self.writes
        self.reads, self.writes = [], []
        return r, w


def config_hash(cfg: dict[str, Any]) -> str:
    """Identity of a configuration, independent of seed and run name."""
    core = {
        "env_id": cfg.get("env_id"),
        "algo": cfg.get("algo"),
        "total_timesteps": cfg.get("total_timesteps"),
        "n_envs": cfg.get("n_envs", 1),
        "hyperparams": cfg.get("hyperparams", {}),
    }
    return stable_hash(core, 10)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for k in list(d.keys()):
        if k.endswith("_json"):
            d[k[:-5]] = jloads(d.pop(k), None)
    return d


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.conn = connect(self.db_path)
        migrate(self.conn)
        self.tracker = MemoryTracker()

    def close(self) -> None:
        self.conn.close()

    # ---- global (not project-owned) ------------------------------------------
    def list_projects(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM projects ORDER BY created_at")]

    def project_exists(self, project_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone() is not None

    def scope(self, project_id: str) -> "ProjectScope":
        if not self.project_exists(project_id):
            raise NotFound(f"unknown project '{project_id}' - run `evo onboard` first")
        return ProjectScope(self, project_id)

    def create_project(self, *, project_id: str, name: str, env_id: str, algo: str,
                       root_dir: str, source_ref: str, source_hash: str) -> None:
        self.conn.execute(
            "INSERT INTO projects(project_id, name, env_id, algo, root_dir, source_ref, source_hash, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (project_id, name, env_id, algo, root_dir, source_ref, source_hash, now_iso()),
        )
        self.tracker.write(f"projects:{project_id}")


class ProjectScope:
    """All memory access for ONE project. Every query filters on project_id."""

    def __init__(self, store: Store, project_id: str):
        self.store = store
        self.conn = store.conn
        self.pid = project_id
        self.t = store.tracker

    # ---- project ---------------------------------------------------------------
    def project(self) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM projects WHERE project_id=?", (self.pid,)).fetchone()
        self.t.read(f"projects:{self.pid}")
        return dict(r)

    @property
    def root_dir(self) -> Path:
        return Path(self.project()["root_dir"])

    # ---- facts (versioned) -------------------------------------------------------
    def facts(self) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT key, value_json FROM project_facts WHERE project_id=? AND status='current' ORDER BY key",
            (self.pid,),
        ).fetchall()
        self.t.read("project_facts:current")
        return {r["key"]: jloads(r["value_json"]) for r in rows}

    def fact(self, key: str, default: Any = None) -> Any:
        return self.facts().get(key, default)

    def fact_rows(self, key: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM project_facts WHERE project_id=?"
        args: list[Any] = [self.pid]
        if key:
            q += " AND key=?"
            args.append(key)
        q += " ORDER BY key, version"
        self.t.read(f"project_facts:history:{key or '*'}")
        return [row_to_dict(r) for r in self.conn.execute(q, args)]

    def add_fact_version(self, key: str, value: Any, *, source_ref: str, proposed_by: str,
                         approved_by: str | None, reason: str | None = None) -> int:
        """Insert a new CURRENT version of a fact and supersede the old one (atomic)."""
        with tx(self.conn):
            old = self.conn.execute(
                "SELECT fact_id, version FROM project_facts WHERE project_id=? AND key=? AND status='current'",
                (self.pid, key),
            ).fetchone()
            version = (old["version"] + 1) if old else 1
            if old:
                self.conn.execute("UPDATE project_facts SET status='superseded' WHERE fact_id=?", (old["fact_id"],))
            cur = self.conn.execute(
                "INSERT INTO project_facts(project_id, key, value_json, version, status, supersedes_id, source_ref,"
                " reason, proposed_by, approved_by, approved_at, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.pid, key, jdumps(value), version, "current", old["fact_id"] if old else None, source_ref,
                 reason, proposed_by, approved_by, now_iso() if approved_by else None, now_iso()),
            )
        self.t.write(f"project_facts:{key}:v{version}")
        return int(cur.lastrowid)

    # ---- runs ----------------------------------------------------------------------
    def runs(self, *, status: str | None = None, config_hash_: str | None = None,
             cycle_id: int | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM runs WHERE project_id=?"
        args: list[Any] = [self.pid]
        if status:
            q += " AND status=?"
            args.append(status)
        if config_hash_:
            q += " AND config_hash=?"
            args.append(config_hash_)
        if cycle_id is not None:
            q += " AND cycle_id=?"
            args.append(cycle_id)
        q += " ORDER BY run_id"
        rows = [row_to_dict(r) for r in self.conn.execute(q, args)]
        self.t.read(f"runs:{len(rows)}")
        return rows

    def run_by_name(self, name: str) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM runs WHERE name=? AND project_id=?", (name, self.pid)).fetchone()
        if r is None:
            other = self.conn.execute("SELECT project_id FROM runs WHERE name=?", (name,)).fetchone()
            if other is not None:
                self.t.read(f"runs:DENIED:{name}")
                raise IsolationError(f"run '{name}' does not belong to project '{self.pid}'")
            raise NotFound(f"run '{name}' not found in project '{self.pid}'")
        self.t.read(f"runs:{name}")
        return row_to_dict(r)

    def run_by_id(self, run_id: int) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if r is None:
            raise NotFound(f"run id {run_id} not found")
        if r["project_id"] != self.pid:
            self.t.read(f"runs:DENIED:{run_id}")
            raise IsolationError(f"run id {run_id} does not belong to project '{self.pid}'")
        self.t.read(f"runs:{r['name']}")
        return row_to_dict(r)

    def upsert_run(self, *, name: str, cfg: dict[str, Any], status: str, folder: str,
                   file_hashes: dict[str, str], launched_by: str, cycle_id: int | None,
                   idempotency_key: str | None = None, notes: dict | None = None,
                   pid: int | None = None) -> int:
        now = now_iso()
        with tx(self.conn):
            existing = self.conn.execute(
                "SELECT run_id, notes_json FROM runs WHERE project_id=? AND name=?", (self.pid, name)
            ).fetchone()
            if existing:
                merged = jloads(existing["notes_json"], {}) or {}
                merged.update(notes or {})
                self.conn.execute(
                    "UPDATE runs SET status=?, file_hashes_json=?, config_json=?, config_hash=?, notes_json=?,"
                    " pid=COALESCE(?, pid), updated_at=? WHERE run_id=?",
                    (status, jdumps(file_hashes), jdumps(cfg), config_hash(cfg), jdumps(merged), pid, now,
                     existing["run_id"]),
                )
                run_id = int(existing["run_id"])
            else:
                cur = self.conn.execute(
                    "INSERT INTO runs(project_id, name, config_hash, config_json, seed, status, folder,"
                    " file_hashes_json, launched_by, idempotency_key, cycle_id, pid, notes_json, created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.pid, name, config_hash(cfg), jdumps(cfg), cfg.get("seed"), status, folder,
                     jdumps(file_hashes), launched_by, idempotency_key, cycle_id, pid, jdumps(notes or {}), now, now),
                )
                run_id = int(cur.lastrowid)
        self.t.write(f"runs:{name}:{status}")
        return run_id

    def set_run_status(self, run_id: int, status: str, notes: dict | None = None) -> None:
        self.run_by_id(run_id)  # isolation check
        row = self.conn.execute("SELECT notes_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        merged = jloads(row["notes_json"], {}) or {}
        merged.update(notes or {})
        self.conn.execute("UPDATE runs SET status=?, notes_json=?, updated_at=? WHERE run_id=?",
                          (status, jdumps(merged), now_iso(), run_id))
        self.t.write(f"runs:{run_id}:{status}")

    # ---- run metrics (versioned) -------------------------------------------------
    def current_metrics(self, run_id: int) -> dict[str, Any] | None:
        r = self.conn.execute(
            "SELECT * FROM run_metrics WHERE run_id=? AND project_id=? AND status='current'", (run_id, self.pid)
        ).fetchone()
        self.t.read(f"run_metrics:{run_id}")
        return row_to_dict(r)

    def metric_rows(self, run_id: int) -> list[dict[str, Any]]:
        self.run_by_id(run_id)
        rows = self.conn.execute(
            "SELECT * FROM run_metrics WHERE run_id=? AND project_id=? ORDER BY version", (run_id, self.pid)
        ).fetchall()
        self.t.read(f"run_metrics:history:{run_id}")
        return [row_to_dict(r) for r in rows]

    def add_metric_version(self, run_id: int, *, eval_points: list[dict], train: dict, source_ref: str,
                           source_hash: str, status: str = "current", reason: str | None = None,
                           approved_by: str | None = None) -> int:
        """status='current' supersedes the previous current version; 'proposed' waits for approval."""
        self.run_by_id(run_id)
        with tx(self.conn):
            last = self.conn.execute(
                "SELECT MAX(version) AS v FROM run_metrics WHERE run_id=?", (run_id,)
            ).fetchone()["v"] or 0
            old = self.conn.execute(
                "SELECT metric_id FROM run_metrics WHERE run_id=? AND status='current'", (run_id,)
            ).fetchone()
            if status == "current" and old:
                self.conn.execute("UPDATE run_metrics SET status='superseded' WHERE metric_id=?", (old["metric_id"],))
            cur = self.conn.execute(
                "INSERT INTO run_metrics(project_id, run_id, version, status, eval_json, train_json, source_ref,"
                " source_hash, supersedes_id, reason, approved_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.pid, run_id, last + 1, status, jdumps(eval_points), jdumps(train), source_ref, source_hash,
                 old["metric_id"] if old else None, reason, approved_by, now_iso()),
            )
        self.t.write(f"run_metrics:{run_id}:v{last + 1}:{status}")
        return int(cur.lastrowid)

    def promote_metric(self, metric_id: int, approved_by: str) -> None:
        with tx(self.conn):
            row = self.conn.execute(
                "SELECT run_id FROM run_metrics WHERE metric_id=? AND project_id=?", (metric_id, self.pid)
            ).fetchone()
            if row is None:
                raise NotFound(f"metric version {metric_id} not in project {self.pid}")
            old = self.conn.execute(
                "SELECT metric_id FROM run_metrics WHERE run_id=? AND status='current'", (row["run_id"],)
            ).fetchone()
            if old:
                self.conn.execute("UPDATE run_metrics SET status='superseded' WHERE metric_id=?", (old["metric_id"],))
            self.conn.execute(
                "UPDATE run_metrics SET status='current', approved_by=?, supersedes_id=? WHERE metric_id=?",
                (approved_by, old["metric_id"] if old else None, metric_id),
            )
        self.t.write(f"run_metrics:{metric_id}:current")

    # ---- diagnoses -------------------------------------------------------------------
    def add_diagnosis(self, *, run_id: int | None, cycle_id: int | None, kind: str, severity: str,
                      detail: dict) -> tuple[int, bool]:
        """Returns (diag_id, created). Re-recording the same finding is a no-op (idempotent)."""
        dh = stable_hash(detail, 16)
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO diagnoses(project_id, run_id, cycle_id, kind, severity, detail_json, detail_hash,"
            " created_at) VALUES(?,?,?,?,?,?,?,?)",
            (self.pid, run_id, cycle_id, kind, severity, jdumps(detail), dh, now_iso()),
        )
        created = cur.rowcount == 1
        row = self.conn.execute(
            "SELECT diag_id FROM diagnoses WHERE project_id=? AND run_id IS ? AND kind=? AND detail_hash=?",
            (self.pid, run_id, kind, dh),
        ).fetchone()
        if created:
            self.t.write(f"diagnoses:{kind}:{row['diag_id']}")
        return int(row["diag_id"]), created

    def supersede_diagnoses(self, run_id: int, kinds: list[str]) -> None:
        q = "UPDATE diagnoses SET status='superseded' WHERE project_id=? AND run_id=? AND status='current' AND kind IN (%s)" % ",".join("?" * len(kinds))
        self.conn.execute(q, (self.pid, run_id, *kinds))

    def diagnoses(self, *, run_id: int | None = None, cycle_id: int | None = None,
                  kinds: list[str] | None = None, include_superseded: bool = False) -> list[dict[str, Any]]:
        q = ("SELECT d.*, r.name AS run_name, r.config_hash AS config_hash FROM diagnoses d"
             " LEFT JOIN runs r ON r.run_id = d.run_id WHERE d.project_id=?")
        args: list[Any] = [self.pid]
        if not include_superseded:
            q += " AND d.status='current'"
        if run_id is not None:
            q += " AND d.run_id=?"
            args.append(run_id)
        if cycle_id is not None:
            q += " AND d.cycle_id=?"
            args.append(cycle_id)
        if kinds:
            q += " AND d.kind IN (%s)" % ",".join("?" * len(kinds))
            args.extend(kinds)
        q += " ORDER BY d.diag_id"
        rows = [row_to_dict(r) for r in self.conn.execute(q, args)]
        self.t.read(f"diagnoses:{len(rows)}")
        return rows

    def diagnosis(self, diag_id: int) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM diagnoses WHERE diag_id=?", (diag_id,)).fetchone()
        if r is None:
            raise NotFound(f"diag {diag_id}")
        if r["project_id"] != self.pid:
            raise IsolationError(f"diag {diag_id} belongs to another project")
        return row_to_dict(r)

    # ---- hypotheses ----------------------------------------------------------------
    def hypotheses(self, status: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM hypotheses WHERE project_id=?"
        args: list[Any] = [self.pid]
        if status:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY hyp_id"
        rows = [row_to_dict(r) for r in self.conn.execute(q, args)]
        self.t.read(f"hypotheses:{status or '*'}:{len(rows)}")
        return rows

    def hypothesis(self, label: str) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM hypotheses WHERE project_id=? AND label=?", (self.pid, label)).fetchone()
        if r is None:
            raise NotFound(f"hypothesis {label} not found in project {self.pid}")
        self.t.read(f"hypotheses:{label}")
        return row_to_dict(r)

    def open_hypothesis(self, *, statement: str, change: dict, evidence: list[str], cycle_id: int,
                        created_by: str) -> str:
        with tx(self.conn):
            # idempotent: the same change already open -> return it
            for h in self.conn.execute(
                "SELECT label, change_json FROM hypotheses WHERE project_id=? AND status='open'", (self.pid,)
            ):
                if jloads(h["change_json"]) == change:
                    return h["label"]
            n = self.conn.execute("SELECT COUNT(*) AS n FROM hypotheses WHERE project_id=?", (self.pid,)).fetchone()["n"]
            label = f"H{n + 1}"
            now = now_iso()
            self.conn.execute(
                "INSERT INTO hypotheses(project_id, label, statement, change_json, status, evidence_json, opened_cycle,"
                " created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (self.pid, label, statement, jdumps(change), "open", jdumps(evidence), cycle_id, created_by, now, now),
            )
        self.t.write(f"hypotheses:{label}:open")
        return label

    def resolve_hypothesis(self, label: str, *, status: str, resolution: dict, cycle_id: int) -> None:
        self.hypothesis(label)
        self.conn.execute(
            "UPDATE hypotheses SET status=?, resolution_json=?, resolved_cycle=?, updated_at=?"
            " WHERE project_id=? AND label=?",
            (status, jdumps(resolution), cycle_id, now_iso(), self.pid, label),
        )
        self.t.write(f"hypotheses:{label}:{status}")

    # ---- lessons ---------------------------------------------------------------------
    def lessons(self, status: str = "current") -> list[dict[str, Any]]:
        rows = [row_to_dict(r) for r in self.conn.execute(
            "SELECT * FROM lessons WHERE project_id=? AND status=? ORDER BY lesson_id", (self.pid, status))]
        self.t.read(f"lessons:{len(rows)}")
        return rows

    def add_lesson(self, *, text: str, kind: str, region: dict, source: dict, approved_by: str | None) -> int:
        existing = self.conn.execute(
            "SELECT lesson_id FROM lessons WHERE project_id=? AND region_json=? AND kind=? AND status='current'",
            (self.pid, jdumps(region), kind),
        ).fetchone()
        if existing:
            return int(existing["lesson_id"])
        cur = self.conn.execute(
            "INSERT INTO lessons(project_id, text, kind, region_json, source_json, status, approved_by, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (self.pid, text, kind, jdumps(region), jdumps(source), "current", approved_by, now_iso()),
        )
        self.t.write(f"lessons:{cur.lastrowid}")
        return int(cur.lastrowid)

    def set_lesson_status(self, lesson_id: int, status: str) -> None:
        cur = self.conn.execute("UPDATE lessons SET status=? WHERE lesson_id=? AND project_id=?",
                                (status, lesson_id, self.pid))
        if cur.rowcount == 0:
            raise NotFound(f"lesson {lesson_id} not in project {self.pid}")
        self.t.write(f"lessons:{lesson_id}:{status}")

    # ---- proposals & approvals --------------------------------------------------------
    def create_proposal(self, *, kind: str, payload: dict, rationale: str, evidence: list[str],
                        cycle_id: int | None, created_by: str) -> tuple[int, bool]:
        ch = stable_hash({"kind": kind, "payload": payload}, 16)
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO proposals(project_id, cycle_id, kind, payload_json, rationale, evidence_json, status,"
            " created_by, created_at, content_hash) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.pid, cycle_id, kind, jdumps(payload), rationale, jdumps(evidence), "pending", created_by,
             now_iso(), ch),
        )
        created = cur.rowcount == 1
        row = self.conn.execute(
            "SELECT proposal_id FROM proposals WHERE project_id=? AND kind=? AND content_hash=? AND cycle_id IS ?",
            (self.pid, kind, ch, cycle_id),
        ).fetchone()
        if created:
            self.t.write(f"proposals:{row['proposal_id']}:{kind}")
        return int(row["proposal_id"]), created

    def proposals(self, status: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM proposals WHERE project_id=?"
        args: list[Any] = [self.pid]
        if status:
            q += " AND status=?"
            args.append(status)
        if kind:
            q += " AND kind=?"
            args.append(kind)
        q += " ORDER BY proposal_id"
        rows = [row_to_dict(r) for r in self.conn.execute(q, args)]
        self.t.read(f"proposals:{status or '*'}:{len(rows)}")
        return rows

    def proposal(self, proposal_id: int) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        if r is None:
            raise NotFound(f"proposal {proposal_id} not found")
        if r["project_id"] != self.pid:
            raise IsolationError(f"proposal {proposal_id} belongs to another project")
        return row_to_dict(r)

    def set_proposal_status(self, proposal_id: int, status: str) -> None:
        self.proposal(proposal_id)
        self.conn.execute("UPDATE proposals SET status=?, decided_at=? WHERE proposal_id=?",
                          (status, now_iso(), proposal_id))
        self.t.write(f"proposals:{proposal_id}:{status}")

    def record_approval(self, *, subject_type: str, subject_id: str, decision: str, comment: str | None,
                        actor: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO approvals(project_id, subject_type, subject_id, decision, comment, actor, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (self.pid, subject_type, subject_id, decision, comment, actor, now_iso()),
        )
        self.t.write(f"approvals:{cur.lastrowid}")
        return int(cur.lastrowid)

    def approvals(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM approvals WHERE project_id=? ORDER BY approval_id", (self.pid,))]

    # ---- cycles (working state checkpoints) ---------------------------------------------
    def latest_cycle(self) -> dict[str, Any] | None:
        r = self.conn.execute(
            "SELECT * FROM cycles WHERE project_id=? ORDER BY number DESC LIMIT 1", (self.pid,)).fetchone()
        self.t.read("cycles:latest")
        return row_to_dict(r)

    def cycles(self) -> list[dict[str, Any]]:
        return [row_to_dict(r) for r in self.conn.execute(
            "SELECT * FROM cycles WHERE project_id=? ORDER BY number", (self.pid,))]

    def cycle(self, cycle_id: int) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM cycles WHERE cycle_id=? AND project_id=?", (cycle_id, self.pid)).fetchone()
        if r is None:
            raise NotFound(f"cycle {cycle_id} not in project {self.pid}")
        return row_to_dict(r)

    def open_cycle(self, *, clock: str, memory_mode: str) -> dict[str, Any]:
        with tx(self.conn):
            n = self.conn.execute("SELECT COALESCE(MAX(number), 0) AS n FROM cycles WHERE project_id=?",
                                  (self.pid,)).fetchone()["n"]
            cur = self.conn.execute(
                "INSERT INTO cycles(project_id, number, status, memory_mode, clock, checkpoint_json, started_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (self.pid, n + 1, "open", memory_mode, clock, "{}", now_iso()),
            )
        self.t.write(f"cycles:{n + 1}:open")
        return self.cycle(int(cur.lastrowid))

    def save_checkpoint(self, cycle_id: int, checkpoint: dict, status: str | None = None) -> None:
        if status:
            self.conn.execute("UPDATE cycles SET checkpoint_json=?, status=? WHERE cycle_id=? AND project_id=?",
                              (jdumps(checkpoint), status, cycle_id, self.pid))
        else:
            self.conn.execute("UPDATE cycles SET checkpoint_json=? WHERE cycle_id=? AND project_id=?",
                              (jdumps(checkpoint), cycle_id, self.pid))
        self.t.write(f"cycles:{cycle_id}:checkpoint")

    def close_cycle(self, cycle_id: int, summary: dict) -> None:
        self.conn.execute(
            "UPDATE cycles SET status='closed', summary_json=?, ended_at=?, checkpoint_json='{}'"
            " WHERE cycle_id=? AND project_id=?",
            (jdumps(summary), now_iso(), cycle_id, self.pid),
        )
        self.t.write(f"cycles:{cycle_id}:closed")

    # ---- decisions (episodic) --------------------------------------------------------------
    def add_decision(self, *, cycle_id: int, step: int, action: str, arguments: dict, rationale: str,
                     evidence: list[str], gate: dict, memory_mode: str, provider: str, model: str,
                     prompt_hash: str, latency_ms: int | None, brief: dict | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO decisions(project_id, cycle_id, step, action, arguments_json, rationale, evidence_json,"
            " gate_json, memory_mode, provider, model, prompt_hash, latency_ms, brief_json, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(cycle_id, step) DO UPDATE SET action=excluded.action, arguments_json=excluded.arguments_json,"
            " rationale=excluded.rationale, evidence_json=excluded.evidence_json, gate_json=excluded.gate_json",
            (self.pid, cycle_id, step, action, jdumps(arguments), rationale, jdumps(evidence), jdumps(gate),
             memory_mode, provider, model, prompt_hash, latency_ms, jdumps(brief) if brief is not None else None,
             now_iso()),
        )
        self.t.write(f"decisions:{cycle_id}:{step}")
        return int(cur.lastrowid)

    def decisions(self, *, cycle_id: int | None = None, last_n: int | None = None) -> list[dict[str, Any]]:
        q = "SELECT d.*, c.number AS cycle_number FROM decisions d JOIN cycles c ON c.cycle_id=d.cycle_id WHERE d.project_id=?"
        args: list[Any] = [self.pid]
        if cycle_id is not None:
            q += " AND d.cycle_id=?"
            args.append(cycle_id)
        q += " ORDER BY d.decision_id DESC"
        if last_n:
            q += " LIMIT ?"
            args.append(last_n)
        rows = [row_to_dict(r) for r in self.conn.execute(q, args)]
        rows.reverse()
        self.t.read(f"decisions:{len(rows)}")
        return rows

    # ---- audit ---------------------------------------------------------------------------
    def action_log(self, tail: int | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM action_log WHERE project_id=? ORDER BY log_id DESC"
        args: list[Any] = [self.pid]
        if tail:
            q += " LIMIT ?"
            args.append(tail)
        rows = [row_to_dict(r) for r in self.conn.execute(q, args)]
        rows.reverse()
        return rows
