"""Tool: search_knowledge - retrieval over cited RL tuning notes.

Notes are Markdown files in knowledge/ with a small YAML header (title,
source_url, version). Each `## ` section is indexed in SQLite FTS5 and ranked
with BM25. No vector database: the corpus is small and keyword search over
technical terms (learning rate, entropy, clip range) works well and is
inspectable. Notes are global and read-only to the agent.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from evo.memory.store import Store
from evo.util import now_iso, sha256_text

_STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "is", "are", "with", "why", "how", "what",
         "does", "do", "my", "it", "this", "that", "be", "at", "by", "from"}


def _parse(path: Path) -> tuple[dict, list[tuple[str, str]]]:
    text = path.read_text(encoding="utf-8")
    meta: dict = {}
    if text.startswith("---"):
        _, head, text = text.split("---", 2)
        meta = yaml.safe_load(head) or {}
    sections: list[tuple[str, str]] = []
    current, buf = "Overview", []
    for line in text.splitlines():
        if line.startswith("## "):
            if "".join(buf).strip():
                sections.append((current, "\n".join(buf).strip()))
            current, buf = line[3:].strip(), []
        elif not line.startswith("# "):
            buf.append(line)
    if "".join(buf).strip():
        sections.append((current, "\n".join(buf).strip()))
    return meta, sections


def index_knowledge(store: Store, kdir: Path) -> dict[str, Any]:
    conn = store.conn
    seen, changed = set(), []
    for path in sorted(kdir.glob("*.md")):
        doc_id = path.stem
        seen.add(doc_id)
        sha = sha256_text(path.read_text(encoding="utf-8"))
        row = conn.execute("SELECT sha256 FROM knowledge_docs WHERE doc_id=?", (doc_id,)).fetchone()
        if row and row["sha256"] == sha:
            continue
        meta, sections = _parse(path)
        conn.execute("DELETE FROM knowledge_fts WHERE doc_id=?", (doc_id,))
        conn.execute(
            "INSERT INTO knowledge_docs(doc_id, path, title, source_url, version, sha256, indexed_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(doc_id) DO UPDATE SET path=excluded.path, title=excluded.title, source_url=excluded.source_url,"
            " version=excluded.version, sha256=excluded.sha256, indexed_at=excluded.indexed_at",
            (doc_id, str(path.name), meta.get("title", doc_id), meta.get("source_url", ""),
             str(meta.get("version", "1")), sha, now_iso()),
        )
        for sec, body in sections:
            conn.execute("INSERT INTO knowledge_fts(doc_id, section, body) VALUES(?,?,?)", (doc_id, sec, body))
        changed.append(doc_id)
    for r in conn.execute("SELECT doc_id FROM knowledge_docs").fetchall():
        if r["doc_id"] not in seen:
            conn.execute("DELETE FROM knowledge_fts WHERE doc_id=?", (r["doc_id"],))
            conn.execute("DELETE FROM knowledge_docs WHERE doc_id=?", (r["doc_id"],))
    return {"indexed": changed, "total_docs": len(seen)}


def _fts_query(q: str) -> str:
    toks = [t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9_]+", q)]
    toks = [t for t in toks if t not in _STOP][:12]
    return " OR ".join(f'"{t}"' for t in toks)


def search(store: Store, query: str, k: int = 4) -> list[dict[str, Any]]:
    fq = _fts_query(query)
    if not fq:
        return []
    rows = store.conn.execute(
        "SELECT f.doc_id, f.section, snippet(knowledge_fts, 2, '[', ']', ' ... ', 40) AS snip,"
        " bm25(knowledge_fts) AS score, d.title, d.source_url, d.version"
        " FROM knowledge_fts f JOIN knowledge_docs d ON d.doc_id = f.doc_id"
        " WHERE knowledge_fts MATCH ? ORDER BY score LIMIT ?",
        (fq, k),
    ).fetchall()
    store.tracker.read(f"knowledge:{len(rows)}")
    return [{"ref": f"kb:{r['doc_id']}#{_slug(r['section'])}", "title": r["title"], "section": r["section"],
             "source_url": r["source_url"], "version": r["version"], "snippet": r["snip"],
             "bm25": round(r["score"], 3)} for r in rows]


def kb_ref_exists(store: Store, ref: str) -> bool:
    m = re.match(r"kb:([^#]+)(?:#(.+))?$", ref)
    if not m:
        return False
    doc = store.conn.execute("SELECT 1 FROM knowledge_docs WHERE doc_id=?", (m.group(1),)).fetchone()
    if not doc:
        return False
    if not m.group(2):
        return True
    secs = store.conn.execute("SELECT section FROM knowledge_fts WHERE doc_id=?", (m.group(1),)).fetchall()
    return any(_slug(s["section"]) == m.group(2) for s in secs)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
