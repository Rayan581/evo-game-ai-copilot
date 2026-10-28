"""Handling of untrusted text (logs, config comments, knowledge notes).

Untrusted text is never interpreted as instructions. Before it reaches the LLM
it is (1) scanned for instruction-like content, which is recorded as a
diagnostic, and (2) wrapped in a fenced, labelled data block. Permissions,
budgets and allowed actions come only from approved facts and code.
"""
from __future__ import annotations

import re

# Phrases that look like someone trying to steer the agent from inside data.
_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) (instructions|rules)",
    r"disregard (the |all )?(previous|prior|above|system)",
    r"you are now",
    r"new instructions?:",
    r"system prompt",
    r"(set|change|increase|raise) (the )?(run |gpu )?budget",
    r"(approve|launch|run) (all|\d+) (runs|experiments)",
    r"without (asking|approval|permission)",
    r"auto[- ]?approve",
    r"grant (yourself|the agent)",
    r"delete (the )?(database|history|records)",
    r"\bact as\b",
]
_RX = re.compile("|".join(f"(?:{p})" for p in _PATTERNS), re.IGNORECASE)

FENCE = "<<<UNTRUSTED_DATA {label}>>>\n{body}\n<<<END_UNTRUSTED_DATA>>>"


def scan(text: str) -> list[dict]:
    """Return instruction-like lines found in text (line number + excerpt)."""
    hits = []
    for i, line in enumerate(text.splitlines(), start=1):
        m = _RX.search(line)
        if m:
            hits.append({"line": i, "match": m.group(0), "excerpt": line.strip()[:200]})
    return hits


def neutralise(text: str, max_chars: int = 4000) -> str:
    """Strip control characters and any attempt to close our fence early."""
    text = text.replace("<<<", "‹‹‹").replace(">>>", "›››")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    if len(text) > max_chars:
        text = text[-max_chars:]
    return text


def fence(label: str, text: str, max_chars: int = 4000) -> str:
    return FENCE.format(label=label, body=neutralise(text, max_chars))
