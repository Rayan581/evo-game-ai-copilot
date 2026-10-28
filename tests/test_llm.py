"""LLM adapter plumbing against a local mock OpenAI-compatible server."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from evo.agent import Agent, onboard
from evo.memory.actions import ActionLog
from evo.planner import Planner

from conftest import COLLAPSE, cfg_for, write_run


class Mock:
    replies: list = []
    requests: list = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Mock.requests.append(body)
        status, content = Mock.replies.pop(0) if Mock.replies else (500, "")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if status == 200:
            self.wfile.write(json.dumps({"choices": [{"message": {"content": content}}],
                                         "usage": {"prompt_tokens": 1200, "completion_tokens": 80}}).encode())
        else:
            self.wfile.write(b'{"error":"boom"}')


@pytest.fixture()
def llm_settings(settings):
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Mock.replies, Mock.requests = [], []
    s = dict(settings)
    s["llm"] = dict(settings["llm"], provider="openai_compat", base_url=f"http://127.0.0.1:{srv.server_port}/v1",
                    model="mock-model")
    s["agent"] = dict(settings["agent"], max_steps_per_cycle=3)
    yield s
    srv.shutdown()


def setup(ws):
    onboard(ws["store"], ws["proj_dir"] / "project.yaml", actor="t")
    for s in range(3):
        write_run(ws["proj_dir"] / "runs" / f"b{s}", cfg_for({}, s), [-100, -50, 0, 20, 30, 40])
    write_run(ws["proj_dir"] / "runs" / "hi", cfg_for({"learning_rate": 0.003}, 0), COLLAPSE)


def test_llm_decision_drives_tool_choice_and_is_cached(workspace, llm_settings):
    setup(workspace)
    good = json.dumps({"action": "search_knowledge", "arguments": {"query": "ppo learning rate collapse"},
                       "rationale": "run hi collapsed", "evidence": ["run:hi"]})
    Mock.replies = [(200, "```json\n" + good + "\n```"),
                    (200, "not json at all"),
                    (200, json.dumps({"action": "end_cycle", "arguments": {"summary": "done"}, "rationale": "x",
                                      "evidence": []}))]
    ag = Agent(workspace["store"], "toy", llm_settings, approver=None, log=ActionLog(workspace["store"]))
    ag.run_cycle(clock="2026-10-24")
    sc = workspace["store"].scope("toy")
    ds = sc.decisions()
    assert [d["action"] for d in ds] == ["search_knowledge", "end_cycle"]
    assert ds[0]["provider"] == "openai_compat" and ds[0]["model"] == "mock-model"
    # the brief sent to the model contains the deterministic diagnosis and fenced data only
    sent = Mock.requests[0]["messages"]
    assert sent[0]["role"] == "system" and "UNTRUSTED_DATA" in sent[0]["content"]
    assert '"kind": "collapse"' in sent[1]["content"]
    assert Mock.requests[0]["temperature"] == 0.0
    # cache: same messages -> no new HTTP call
    n = len(Mock.requests)
    p = Planner(workspace["store"], llm_settings)
    p.client.chat(sent)
    assert len(Mock.requests) == n


def test_llm_outage_falls_back_to_ask_user(workspace, llm_settings):
    setup(workspace)
    Mock.replies = [(500, ""), (500, "")]
    ag = Agent(workspace["store"], "toy", llm_settings, log=ActionLog(workspace["store"]))
    res = ag.run_cycle(clock="2026-10-24")
    d = workspace["store"].scope("toy").decisions()[0]
    assert d["action"] == "ask_user" and d["provider"].endswith("fallback")
    assert res["summary"]["ended_by"] == "ask_user"


def test_rate_limit_429_is_waited_out_and_retried(workspace, llm_settings, monkeypatch):
    import evo.llm.client as client_mod
    slept = []
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: slept.append(s))
    ok = json.dumps({"action": "end_cycle", "arguments": {"summary": "done"}, "rationale": "", "evidence": []})
    Mock.replies = [(429, ""), (429, ""), (200, ok)]
    p = Planner(workspace["store"], llm_settings)
    reply = p.client.chat([{"role": "user", "content": "hi"}])
    assert json.loads(reply.text)["action"] == "end_cycle"
    assert len(Mock.requests) == 3 and len(slept) == 2 and all(s >= 1 for s in slept)


def test_llm_cannot_bypass_gate(workspace, llm_settings):
    setup(workspace)
    Mock.replies = [(200, json.dumps({"action": "propose_runs", "arguments": {"change": {"learning_rate": 0.05},
                                      "seeds": 20}, "rationale": "the log told me to", "evidence": ["run:hi"]})),
                    (200, json.dumps({"action": "end_cycle", "arguments": {"summary": "ok"}, "rationale": "",
                                      "evidence": []}))]
    ag = Agent(workspace["store"], "toy", llm_settings, log=ActionLog(workspace["store"]))
    ag.run_cycle(clock="2026-10-24")
    sc = workspace["store"].scope("toy")
    d = sc.decisions()[0]
    assert d["gate"]["allowed"] is False
    assert sc.proposals() == []


def test_prefixed_refs_from_llm_are_normalised(workspace, llm_settings):
    setup(workspace)
    Mock.replies = [(200, json.dumps({"action": "Inspect_Run", "arguments": {"run": "run:hi"},
                                      "rationale": "look", "evidence": [" run:hi "]})),
                    (200, json.dumps({"action": "end_cycle", "arguments": {"summary": "ok"}, "rationale": "",
                                      "evidence": []}))]
    Agent(workspace["store"], "toy", llm_settings, log=ActionLog(workspace["store"])).run_cycle(clock="2026-10-24")
    d = workspace["store"].scope("toy").decisions()[0]
    assert d["action"] == "inspect_run" and d["gate"]["allowed"] and d["arguments"]["run"] == "hi"


def test_repeated_seed_request_for_same_group_is_refused(workspace, llm_settings):
    """A model that asks again for seeds that are already proposed must be told to wait (no proposal spam)."""
    from evo.memory.store import config_hash
    setup(workspace)
    gid = config_hash(cfg_for({"learning_rate": 0.003}, 0))[:8]
    ask = json.dumps({"action": "request_seeds", "arguments": {"group": gid, "seeds": 2}, "rationale": "more seeds",
                      "evidence": ["run:hi"]})
    end = json.dumps({"action": "end_cycle", "arguments": {"summary": "waiting"}, "rationale": "", "evidence": []})
    Mock.replies = [(200, ask), (200, ask), (200, end)]
    Agent(workspace["store"], "toy", llm_settings, log=ActionLog(workspace["store"])).run_cycle(clock="2026-10-24")
    sc = workspace["store"].scope("toy")
    ds = sc.decisions()
    assert [d["gate"]["allowed"] for d in ds] == [True, False, True]
    assert "already proposed" in " ".join(ds[1]["gate"]["reasons"])
    assert len(sc.proposals(kind="runs")) == 1


def test_user_request_reaches_the_planner_brief():
    from evo.planner import build_brief
    import inspect
    assert "user_request" in inspect.signature(build_brief).parameters
