"""Audited, retry-safe tool execution.

`ActionLog.call` wraps every tool invocation:
  * writes an action_log row (running -> succeeded / failed / skipped_duplicate / refused)
  * records which memory the call read and wrote (from the MemoryTracker)
  * for side effects, consults the `side_effects` ledger by idempotency key so a
    retry or a re-run of the same step never repeats the effect
  * retries transient failures with backoff
"""
from __future__ import annotations

import time
import traceback
from typing import Any, Callable

from evo.memory.store import Store
from evo.safety.faults import FaultInjector, TransientToolError
from evo.util import jdumps, jloads, now_iso


class ToolRefused(Exception):
    """A tool declined to act (policy, isolation, validation). Not retried."""


class ToolFailed(Exception):
    def __init__(self, tool: str, error: str, log_id: int):
        super().__init__(f"{tool} failed: {error}")
        self.tool = tool
        self.error = error
        self.log_id = log_id


class ActionLog:
    def __init__(self, store: Store, faults: FaultInjector | None = None, max_retries: int = 2,
                 backoff_s: float = 0.2):
        self.store = store
        self.conn = store.conn
        self.faults = faults or FaultInjector.from_env()
        self.max_retries = max_retries
        self.backoff_s = backoff_s

    # ------------------------------------------------------------------------------
    def _start(self, *, project_id, cycle_id, step, tool, args, key, attempt) -> int:
        cur = self.conn.execute(
            "INSERT INTO action_log(project_id, cycle_id, step, tool, args_json, idempotency_key, attempt, status,"
            " started_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (project_id, cycle_id, step, tool, jdumps(args), key, attempt, "running", now_iso()),
        )
        return int(cur.lastrowid)

    def _finish(self, log_id: int, status: str, result: Any = None, error: str | None = None) -> None:
        reads, writes = self.store.tracker.drain()
        self.conn.execute(
            "UPDATE action_log SET status=?, result_json=?, error=?, memory_reads_json=?, memory_writes_json=?,"
            " ended_at=? WHERE log_id=?",
            (status, jdumps(result) if result is not None else None, error, jdumps(reads), jdumps(writes),
             now_iso(), log_id),
        )

    # ------------------------------------------------------------------------------
    def side_effect_state(self, key: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM side_effects WHERE idempotency_key=?", (key,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["result"] = jloads(d.pop("result_json"), None)
        return d

    def _claim(self, key: str, project_id: str, tool: str) -> None:
        now = now_iso()
        self.conn.execute(
            "INSERT INTO side_effects(idempotency_key, project_id, tool, status, attempts, created_at, updated_at)"
            " VALUES(?,?,?,?,1,?,?) ON CONFLICT(idempotency_key) DO UPDATE SET status='running',"
            " attempts=attempts+1, updated_at=excluded.updated_at",
            (key, project_id, tool, "running", now, now),
        )

    def _settle(self, key: str, status: str, result: Any) -> None:
        self.conn.execute(
            "UPDATE side_effects SET status=?, result_json=?, updated_at=? WHERE idempotency_key=?",
            (status, jdumps(result) if result is not None else None, now_iso(), key),
        )

    # ------------------------------------------------------------------------------
    def call(self, tool: str, args: dict[str, Any], fn: Callable[[], Any], *, project_id: str | None,
             cycle_id: int | None = None, step: int | None = None, idempotency_key: str | None = None,
             retry: bool = True) -> Any:
        """Run `fn` as an audited tool call. Returns fn's result.

        With an idempotency key, a previously *succeeded* effect is returned from the
        ledger instead of being executed again (logged as skipped_duplicate).
        """
        if idempotency_key:
            prev = self.side_effect_state(idempotency_key)
            if prev and prev["status"] == "succeeded":
                log_id = self._start(project_id=project_id, cycle_id=cycle_id, step=step, tool=tool, args=args,
                                     key=idempotency_key, attempt=prev["attempts"])
                self._finish(log_id, "skipped_duplicate", result=prev["result"])
                return prev["result"]

        attempt = 0
        while True:
            attempt += 1
            self.store.tracker.drain()  # isolate this call's memory trace
            log_id = self._start(project_id=project_id, cycle_id=cycle_id, step=step, tool=tool, args=args,
                                 key=idempotency_key, attempt=attempt)
            if idempotency_key:
                self._claim(idempotency_key, project_id or "-", tool)
            try:
                self.faults.maybe_fail(tool, phase="before")
                result = fn()
                self.faults.maybe_fail(tool, phase="after")
            except ToolRefused as e:
                self._finish(log_id, "refused", error=str(e))
                if idempotency_key:
                    self._settle(idempotency_key, "failed", {"refused": str(e)})
                raise
            except TransientToolError as e:
                self._finish(log_id, "failed", error=f"transient: {e}")
                if idempotency_key:
                    self._settle(idempotency_key, "failed", None)
                if retry and attempt <= self.max_retries:
                    time.sleep(self.backoff_s * attempt)
                    continue
                raise ToolFailed(tool, f"transient: {e}", log_id) from e
            except Exception as e:  # permanent failure: report, keep state, no retry
                err = f"{type(e).__name__}: {e}"
                self._finish(log_id, "failed", error=err + "\n" + traceback.format_exc(limit=3))
                if idempotency_key:
                    self._settle(idempotency_key, "failed", None)
                raise ToolFailed(tool, err, log_id) from e
            self._finish(log_id, "succeeded", result=_summarise(result))
            if idempotency_key:
                self._settle(idempotency_key, "succeeded", _summarise(result))
            return result

    def note(self, tool: str, args: dict, *, project_id: str | None, cycle_id: int | None = None,
             step: int | None = None, status: str = "succeeded", result: Any = None, error: str | None = None) -> int:
        """Log an event that is not a function call (e.g. an isolation denial or gate refusal)."""
        log_id = self._start(project_id=project_id, cycle_id=cycle_id, step=step, tool=tool, args=args, key=None,
                             attempt=1)
        self._finish(log_id, status, result=result, error=error)
        return log_id


def _summarise(result: Any) -> Any:
    """Keep action_log rows small: long lists are truncated."""
    if isinstance(result, dict):
        return {k: _summarise(v) for k, v in result.items()}
    if isinstance(result, list) and len(result) > 20:
        return [_summarise(v) for v in result[:20]] + [f"... {len(result) - 20} more"]
    if isinstance(result, str) and len(result) > 2000:
        return result[:2000] + "...[truncated]"
    return result
