from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(obj: Any) -> Any:
    """Make an object JSON-safe: NaN/inf -> None, tuples -> lists, numpy -> python."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if hasattr(obj, "item") and callable(obj.item):  # numpy scalar
        try:
            obj = obj.item()
        except Exception:
            pass
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def jdumps(obj: Any) -> str:
    return json.dumps(_clean(obj), sort_keys=True, ensure_ascii=False)


def jloads(s: str | None, default: Any = None) -> Any:
    if s is None or s == "":
        return default
    return json.loads(s)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_hash(obj: Any, n: int = 12) -> str:
    return sha256_text(jdumps(obj))[:n]
