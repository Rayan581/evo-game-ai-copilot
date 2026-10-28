-- EVO persistent memory schema (SQLite). evo.db is the authoritative store.
-- Every project-owned row carries project_id; every versioned row carries
-- version / status / supersedes_id so corrections never destroy history.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Long-term: stable project identity -----------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    project_id  TEXT PRIMARY KEY,           -- stable slug, e.g. "lander"
    name        TEXT NOT NULL,
    env_id      TEXT NOT NULL,
    algo        TEXT NOT NULL,
    root_dir    TEXT NOT NULL,              -- folder holding runs/ and reports/
    source_ref  TEXT NOT NULL,              -- path of the onboarding file
    source_hash TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Long-term, versioned: approved project facts --------------------------------
CREATE TABLE IF NOT EXISTS project_facts (
    fact_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    TEXT NOT NULL REFERENCES projects(project_id),
    key           TEXT NOT NULL,
    value_json    TEXT NOT NULL,
    version       INTEGER NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('current','superseded','proposed','rejected')),
    supersedes_id INTEGER REFERENCES project_facts(fact_id),
    source_ref    TEXT NOT NULL,
    reason        TEXT,
    proposed_by   TEXT NOT NULL,            -- 'onboarding' | 'user' | 'agent'
    approved_by   TEXT,
    approved_at   TEXT,
    created_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_fact_current
    ON project_facts(project_id, key) WHERE status = 'current';

-- Long-term: runs (one folder = one seed of one config) -----------------------
CREATE TABLE IF NOT EXISTS runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL REFERENCES projects(project_id),
    name            TEXT NOT NULL,          -- folder name, stable reference "run:<name>"
    config_hash     TEXT NOT NULL,          -- hash of hyperparameters, seed excluded
    config_json     TEXT NOT NULL,
    seed            INTEGER,
    status          TEXT NOT NULL CHECK (status IN ('queued','running','completed','incomplete','failed')),
    folder          TEXT NOT NULL,
    file_hashes_json TEXT NOT NULL DEFAULT '{}',
    launched_by     TEXT NOT NULL DEFAULT 'user',   -- 'user' | 'agent'
    idempotency_key TEXT UNIQUE,
    cycle_id        INTEGER,                -- cycle in which it was first ingested / launched
    pid             INTEGER,
    notes_json      TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (project_id, name)
);
CREATE INDEX IF NOT EXISTS ix_runs_project_hash ON runs(project_id, config_hash);

-- Long-term, versioned: the evaluation curve of a run (the source of scores) ---
CREATE TABLE IF NOT EXISTS run_metrics (
    metric_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    TEXT NOT NULL REFERENCES projects(project_id),
    run_id        INTEGER NOT NULL REFERENCES runs(run_id),
    version       INTEGER NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('current','superseded','proposed','rejected')),
    eval_json     TEXT NOT NULL,            -- [{"step":..,"mean":..,"std":..,"n":..}, ...]
    train_json    TEXT NOT NULL DEFAULT '{}', -- compact training stats (nan flags, max kl, ...)
    source_ref    TEXT NOT NULL,
    source_hash   TEXT NOT NULL,
    supersedes_id INTEGER REFERENCES run_metrics(metric_id),
    reason        TEXT,
    approved_by   TEXT,
    created_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_metric_current
    ON run_metrics(run_id) WHERE status = 'current';

-- Long-term: deterministic findings about runs ---------------------------------
CREATE TABLE IF NOT EXISTS diagnoses (
    diag_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL REFERENCES projects(project_id),
    run_id      INTEGER REFERENCES runs(run_id),
    cycle_id    INTEGER,
    kind        TEXT NOT NULL,              -- collapse | plateau | divergence | incomplete | failed | untrusted_instruction | source_changed
    severity    TEXT NOT NULL,              -- info | warning | critical
    detail_json TEXT NOT NULL,
    detail_hash TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'current' CHECK (status IN ('current','superseded')),
    created_at  TEXT NOT NULL,
    UNIQUE (project_id, run_id, kind, detail_hash)
);

-- Long-term: hypotheses and what happened to them -----------------------------
CREATE TABLE IF NOT EXISTS hypotheses (
    hyp_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL REFERENCES projects(project_id),
    label           TEXT NOT NULL,          -- H1, H2 ... per project
    statement       TEXT NOT NULL,
    change_json     TEXT NOT NULL,          -- hyperparameter overrides relative to the baseline
    status          TEXT NOT NULL CHECK (status IN ('open','supported','refuted','inconclusive')),
    evidence_json   TEXT NOT NULL DEFAULT '[]',
    resolution_json TEXT,
    opened_cycle    INTEGER,
    resolved_cycle  INTEGER,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (project_id, label)
);

-- Long-term: distilled outcomes that steer later proposals ----------------------
CREATE TABLE IF NOT EXISTS lessons (
    lesson_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    TEXT NOT NULL REFERENCES projects(project_id),
    text          TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('avoid','prefer')),
    region_json   TEXT NOT NULL,            -- {"learning_rate": {"gte": 0.003}}
    source_json   TEXT NOT NULL,            -- hypothesis / diagnosis / runs it came from
    status        TEXT NOT NULL CHECK (status IN ('current','superseded','rejected')),
    approved_by   TEXT,
    created_at    TEXT NOT NULL
);

-- Approval queue: every action that needs the user ------------------------------
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    TEXT NOT NULL REFERENCES projects(project_id),
    cycle_id      INTEGER,
    kind          TEXT NOT NULL CHECK (kind IN ('runs','resolve_hypothesis','fact_change','metric_correction')),
    payload_json  TEXT NOT NULL,
    rationale     TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','executed','failed')),
    created_by    TEXT NOT NULL,            -- 'agent' | 'user'
    created_at    TEXT NOT NULL,
    decided_at    TEXT,
    content_hash  TEXT NOT NULL,
    UNIQUE (project_id, kind, content_hash, cycle_id)
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT NOT NULL REFERENCES projects(project_id),
    subject_type TEXT NOT NULL,             -- proposal | onboarding
    subject_id   TEXT NOT NULL,
    decision     TEXT NOT NULL CHECK (decision IN ('approved','rejected')),
    comment      TEXT,
    actor        TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- Working state checkpoint + episodic summaries ---------------------------------
CREATE TABLE IF NOT EXISTS cycles (
    cycle_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL REFERENCES projects(project_id),
    number          INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('open','awaiting_approval','closed')),
    memory_mode     TEXT NOT NULL DEFAULT 'on',
    clock           TEXT NOT NULL,          -- simulated date of the review
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    summary_json    TEXT,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    UNIQUE (project_id, number)
);

-- Episodic: every planner step ----------------------------------------------------
CREATE TABLE IF NOT EXISTS decisions (
    decision_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     TEXT NOT NULL REFERENCES projects(project_id),
    cycle_id       INTEGER NOT NULL REFERENCES cycles(cycle_id),
    step           INTEGER NOT NULL,
    action         TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    rationale      TEXT NOT NULL,
    evidence_json  TEXT NOT NULL,
    gate_json      TEXT NOT NULL,           -- {"allowed": bool, "reasons": [...]}
    memory_mode    TEXT NOT NULL,
    provider       TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_hash    TEXT NOT NULL,
    latency_ms     INTEGER,
    brief_json     TEXT,                    -- exactly what the planner saw (audit)
    created_at     TEXT NOT NULL,
    UNIQUE (cycle_id, step)
);

-- Audit: every tool call, with memory reads / writes ---------------------------
CREATE TABLE IF NOT EXISTS action_log (
    log_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id         TEXT,
    cycle_id           INTEGER,
    step               INTEGER,
    tool               TEXT NOT NULL,
    args_json          TEXT NOT NULL,
    idempotency_key    TEXT,
    attempt            INTEGER NOT NULL DEFAULT 1,
    status             TEXT NOT NULL CHECK (status IN ('running','succeeded','failed','skipped_duplicate','refused')),
    result_json        TEXT,
    error              TEXT,
    memory_reads_json  TEXT NOT NULL DEFAULT '[]',
    memory_writes_json TEXT NOT NULL DEFAULT '[]',
    started_at         TEXT NOT NULL,
    ended_at           TEXT
);
CREATE INDEX IF NOT EXISTS ix_log_project ON action_log(project_id, cycle_id);

-- Side effects keyed by idempotency key: the duplicate-prevention ledger ----------
CREATE TABLE IF NOT EXISTS side_effects (
    idempotency_key TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    tool            TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('running','succeeded','failed')),
    result_json     TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Source knowledge (global, read-only to the agent) -----------------------------
CREATE TABLE IF NOT EXISTS knowledge_docs (
    doc_id     TEXT PRIMARY KEY,            -- file stem
    path       TEXT NOT NULL,
    title      TEXT NOT NULL,
    source_url TEXT NOT NULL,
    version    TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    doc_id UNINDEXED, section, body, tokenize = 'porter unicode61'
);

-- LLM response cache (makes evaluation replayable) -------------------------------
CREATE TABLE IF NOT EXISTS llm_cache (
    prompt_hash       TEXT PRIMARY KEY,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    response_text     TEXT NOT NULL,
    latency_ms        INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    created_at        TEXT NOT NULL
);
