import pytest

from evo.memory.actions import ActionLog, ToolFailed
from evo.safety.faults import FaultInjector


def test_side_effect_runs_once_even_when_called_twice(workspace):
    store = workspace["store"]
    effects = []
    log = ActionLog(store, faults=FaultInjector())
    for _ in range(3):
        log.call("send", {"x": 1}, lambda: effects.append(1) or {"ok": True}, project_id="p",
                 idempotency_key="send:p:1")
    assert effects == [1]
    statuses = [r["status"] for r in store.conn.execute("SELECT status FROM action_log ORDER BY log_id")]
    assert statuses == ["succeeded", "skipped_duplicate", "skipped_duplicate"]


def test_transient_failure_is_retried(workspace):
    store = workspace["store"]
    log = ActionLog(store, faults=FaultInjector.parse("work:before:2"), backoff_s=0)
    calls = []
    assert log.call("work", {}, lambda: calls.append(1) or "done", project_id="p") == "done"
    assert calls == [1]
    statuses = [r["status"] for r in store.conn.execute("SELECT status FROM action_log ORDER BY log_id")]
    assert statuses == ["failed", "failed", "succeeded"]


def test_failure_after_side_effect_does_not_duplicate_with_effect_level_check(workspace):
    """The fault fires AFTER the effect happened; the retry must detect the effect, not redo it."""
    store = workspace["store"]
    log = ActionLog(store, faults=FaultInjector.parse("launch:after:1"), backoff_s=0)
    launched = set()

    def launch():
        if "run-a" in launched:          # effect-level idempotency (like a DONE marker / run folder)
            return {"status": "reused"}
        launched.add("run-a")
        return {"status": "launched"}

    res = log.call("launch", {}, launch, project_id="p", idempotency_key="launch:p:run-a")
    assert res == {"status": "reused"} and launched == {"run-a"}


def test_permanent_failure_is_reported_and_not_retried(workspace):
    store = workspace["store"]
    log = ActionLog(store, faults=FaultInjector(), backoff_s=0)
    n = []

    def boom():
        n.append(1)
        raise ValueError("bad input")

    with pytest.raises(ToolFailed):
        log.call("x", {}, boom, project_id="p", idempotency_key="x:1")
    assert n == [1]
    se = store.conn.execute("SELECT status FROM side_effects WHERE idempotency_key='x:1'").fetchone()
    assert se["status"] == "failed"  # a later retry is allowed
