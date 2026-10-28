import pytest

from evo.agent import onboard
from evo.memory.actions import ActionLog
from evo.memory.store import IsolationError, Store

from conftest import PROJECT, cfg_for


def _onboard(ws):
    return onboard(ws["store"], ws["proj_dir"] / "project.yaml", actor="tester")


def test_onboard_creates_versioned_facts_and_is_idempotent(workspace):
    r = _onboard(workspace)
    assert r["created"]
    sc = workspace["store"].scope("toy")
    assert sc.fact("run_budget") == 9
    assert all(f["version"] == 1 and f["status"] == "current" for f in sc.fact_rows())
    again = _onboard(workspace)  # a retried onboarding must not duplicate anything
    assert again["created"] is False or again.get("project_id") == "toy"
    assert len(sc.fact_rows("run_budget")) == 1


def test_fact_correction_supersedes_but_keeps_history(workspace):
    _onboard(workspace)
    sc = workspace["store"].scope("toy")
    sc.add_fact_version("run_budget", 6, source_ref="test", proposed_by="user", approved_by="user", reason="cut")
    rows = sc.fact_rows("run_budget")
    assert [(r["version"], r["status"], r["value"]) for r in rows] == [(1, "superseded", 9), (2, "current", 6)]
    assert rows[1]["supersedes_id"] == rows[0]["fact_id"]
    assert sc.fact("run_budget") == 6


def test_state_survives_a_new_process(workspace):
    _onboard(workspace)
    sc = workspace["store"].scope("toy")
    sc.open_hypothesis(statement="s", change={"learning_rate": 0.001}, evidence=[], cycle_id=1, created_by="t")
    path = workspace["store"].db_path
    workspace["store"].close()
    fresh = Store(path)  # simulates a full restart
    assert fresh.scope("toy").hypothesis("H1")["change"] == {"learning_rate": 0.001}
    workspace["store"] = fresh


def test_isolation_between_projects(workspace):
    _onboard(workspace)
    store = workspace["store"]
    store.create_project(project_id="other", name="o", env_id="GridDungeon-v0", algo="DQN",
                         root_dir=str(workspace["root"]), source_ref="x", source_hash="x")
    toy = store.scope("toy")
    rid = toy.upsert_run(name="toy-secret-s0", cfg=cfg_for({}, 0), status="completed", folder="f",
                         file_hashes={}, launched_by="user", cycle_id=None)
    other = store.scope("other")
    with pytest.raises(IsolationError):
        other.run_by_name("toy-secret-s0")
    with pytest.raises(IsolationError):
        other.run_by_id(rid)
    assert other.runs() == []
    assert other.hypotheses() == [] and other.lessons() == []


def test_proposals_are_idempotent(workspace):
    _onboard(workspace)
    sc = workspace["store"].scope("toy")
    a, created_a = sc.create_proposal(kind="runs", payload={"runs": [1]}, rationale="r", evidence=[], cycle_id=1,
                                      created_by="agent")
    b, created_b = sc.create_proposal(kind="runs", payload={"runs": [1]}, rationale="r", evidence=[], cycle_id=1,
                                      created_by="agent")
    assert a == b and created_a and not created_b
    assert len(sc.proposals()) == 1


def test_action_log_records_memory_reads_and_writes(workspace):
    _onboard(workspace)
    store = workspace["store"]
    sc = store.scope("toy")
    log = ActionLog(store)
    log.call("probe", {}, lambda: sc.facts(), project_id="toy")
    row = sc.action_log()[-1]
    assert row["tool"] == "probe" and row["status"] == "succeeded"
    assert "project_facts:current" in row["memory_reads"]
