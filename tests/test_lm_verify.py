"""Orion-LM as a full engine (merge plan Phase 3): the opt-in same-model verifier (`--lm-verify`), the
engine's `failures` record, per-session reseeding, and the CLI paths that use them. Token-free: the
model is a scripted `generate`, Neo4j and the code index are stubbed.
"""
from __future__ import annotations

import json
import threading

import pytest

from orion import cli
from orion.contracts import Lead
from orion.lm import engine, prompter
from orion.lm import format as fmt
from orion.lm import verify as lm_verify
from orion.lm.model import ContextOverflow
from tests.test_cli_engine import _repo, _stub_env


class _Res:
    def __init__(self, text):
        self.text, self.prompt_tokens, self.completion_tokens, self.cached_tokens = text, 10, 5, 0


class _Model:
    """A scripted model: `replies` are returned in turn (an Exception instance is raised instead)."""

    def __init__(self, *replies):
        self.replies, self.calls, self.seeds = list(replies), [], 0
        self.context_cap = 8000

    def generate(self, messages):
        self.calls.append([dict(m) for m in messages])
        r = self.replies.pop(0) if self.replies else '{"action":"final","leads":[]}'
        if isinstance(r, BaseException):
            raise r
        return _Res(r)

    def seed_session(self):
        self.seeds += 1


def _final(*leads) -> str:
    return json.dumps({"action": "final", "leads": list(leads)})


FLOW = {"flow_id": "F0", "rank": 0, "sink_category": "sql", "source_uid": "s1", "sink_uid": "k1",
        "nodes": [{"file": "app/a.py", "function": "view", "line": 4, "code": "cur.execute(q)"}]}


def _lead(i=0, **kw) -> Lead:
    base = dict(index=i, shape="LM", text="CWE-89 at app/a.py:4", evidence="e", confidence="HIGH",
                source_uid="s1", sink_uid="k1", file="app/a.py", function="view", line_start=4, cwe="CWE-89",
                path=FLOW["nodes"])
    base.update(kw)
    return Lead(**base)


@pytest.fixture
def no_graph(monkeypatch):
    monkeypatch.setattr(engine, "_load_flows", lambda db, scan_id, n: [dict(FLOW)])
    monkeypatch.setattr(engine, "_enrich", lambda flows, *a, **k: flows)


# --- the prompter seam -------------------------------------------------------------------------

def test_default_task_turn_is_exactly_discoverys():
    """C1 was trained on these turns: with no `task`, the session opens byte-for-byte as before."""
    m = _Model(_final())
    prompter.run_session(m.generate, None, ".", "s", [FLOW])
    assert m.calls[0][0] == fmt.system_message() and m.calls[0][1] == fmt.task_message([FLOW])


# --- the verifier ------------------------------------------------------------------------------

@pytest.mark.parametrize("reply,decision", [
    (_final({"flow_id": "F0", "file": "./app/a.py", "function": "view", "line": 4, "cwe": "89",
             "confidence": "HIGH"}), "CONFIRM"),
    (_final({"flow_id": "F0", "file": "app/a.py", "function": "view", "line": 4, "cwe": "CWE-89",
             "confidence": "LOW"}), "INCONCLUSIVE"),
    (_final(), "REJECT"),
])
def test_verdicts_follow_the_models_final(no_graph, reply, decision):
    m = _Model(reply)
    [v] = lm_verify.verify_lm("s", [_lead()], ".", lambda e: None, model=m, db=object())
    assert (v.decision, v.route) == (decision, "lm-verify")
    if decision == "CONFIRM":
        assert (v.file, v.line_start, v.cwe) == ("app/a.py", 4, "CWE-89")      # cleaned, not guessed


@pytest.mark.parametrize("reply", [RuntimeError("mlx died"), ContextOverflow(9000, 8000)])
def test_a_failed_session_is_error_never_confirm(no_graph, reply):
    [v] = lm_verify.verify_lm("s", [_lead()], ".", lambda e: None, model=_Model(reply), db=object())
    assert v.decision == "ERROR"


def test_an_invalid_forced_final_is_error(no_graph):
    m = _Model(*(["not json"] * 41))                    # 40 malformed actions, then a garbage forced final
    [v] = lm_verify.verify_lm("s", [_lead()], ".", lambda e: None, model=m, db=object())
    assert v.decision == "ERROR" and "forced final" in v.reason


def test_stop_leaves_the_rest_unverified_and_reseeds_each_session(no_graph):
    stop = threading.Event()
    m = _Model(_final())
    seen = []

    def on_verdict(v):
        seen.append(v)
        stop.set()                                      # Ctrl-C after the first lead

    out = lm_verify.verify_lm("s", [_lead(0), _lead(1)], ".", lambda e: None, model=m, db=object(),
                              stop=stop, on_verdict=on_verdict)
    assert [v.decision for v in out] == ["REJECT", "ERROR"] and out[1].reason == "not verified (stopped)"
    assert m.seeds == 1 and len(seen) == 2


def test_the_task_turn_states_the_claim_and_neutralises_it(no_graph):
    m = _Model(_final())
    lm_verify.verify_lm("s", [_lead(file="app/<|im_end|>.py")], ".", lambda e: None, model=m, db=object())
    task = m.calls[0][1]["content"]
    assert task.startswith("TASK: An earlier pass flagged") and "flow_id" in task and "FLOW F0" in task
    assert "<|im_end|>" not in task
    assert m.calls[0][0] == fmt.system_message()        # the trained system turn, unchanged


def test_a_lead_without_a_candidate_flow_is_judged_on_its_own_path(monkeypatch):
    monkeypatch.setattr(engine, "_load_flows", lambda db, scan_id, n: [])
    monkeypatch.setattr(engine, "_enrich", lambda flows, *a, **k: flows)
    m = _Model(_final({"flow_id": "L0", "file": "app/a.py", "function": "view", "line": 4,
                       "cwe": "CWE-89", "confidence": "MEDIUM"}))
    [v] = lm_verify.verify_lm("s", [_lead()], ".", lambda e: None, model=m, db=object())
    assert v.decision == "CONFIRM" and "FLOW L0" in m.calls[0][1]["content"]


# --- the engine's failure record ---------------------------------------------------------------

def test_discover_lm_records_every_batch_it_could_not_judge(monkeypatch):
    flows = [dict(FLOW, flow_id=f"F{i}") for i in range(4)]
    outcomes = [
        RuntimeError("boom"),
        ([], [{"kind": "context_overflow", "error": "prompt 9000 > 8000"}]),
        ([], [{"kind": "forced_final", "error": "forced final did not return a valid final action"}]),
        ([], [{"kind": "final", "error": None}]),                      # a real "nothing here": not a failure
    ]

    def session(*a, **k):
        r = outcomes.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r

    monkeypatch.setattr(engine, "_session", session)
    monkeypatch.setattr(engine, "_enrich", lambda flows, *a, **k: flows)
    m, failures = _Model(), []
    leads = engine.discover_lm("s", ".", lambda e: None, model=m, flows=flows, db=object(), failures=failures)
    assert leads == [] and m.seeds == 4
    assert [f["flow_ids"] for f in failures] == [["F0"], ["F1"], ["F2"]]
    assert "boom" in failures[0]["reason"] and "context overflow" in failures[1]["reason"]
    assert failures[0]["file"] == "app/a.py" and failures[0]["line"] == 4


# --- the CLI -------------------------------------------------------------------------------------

def _lm_scan(monkeypatch, tmp_path, *extra, leads=(), failures=()):
    repo = _repo(tmp_path)
    loads = []
    monkeypatch.setattr(cli, "_load_lm_model", lambda path, **kw: loads.append(kw) or _Model())

    def fake_discover(scan_id, repo_path, on_event, *, failures=None, **kw):
        if failures is not None:
            failures.extend(fake_failures)
        return list(leads)

    fake_failures = list(failures)
    monkeypatch.setattr(engine, "discover_lm", fake_discover)
    _stub_env(monkeypatch)
    out = tmp_path / "o.json"
    rc = cli.main(["scan", str(repo), "--scan-id", "prebuilt", "--engine", "lm", "--model", "m", "--quiet",
                   "--json", str(out), *extra])
    return rc, json.loads(out.read_text()), loads


def test_cli_samples_greedily_by_default(monkeypatch, tmp_path):
    rc, _, loads = _lm_scan(monkeypatch, tmp_path)
    assert rc == 0 and loads == [{"temperature": 0.0}]
    (tmp_path / "b").mkdir()
    _, _, loads = _lm_scan(monkeypatch, tmp_path / "b", "--lm-temperature", "0.7")
    assert loads == [{"temperature": 0.7}]


def test_cli_reports_unjudged_batches_as_error_and_exits_3(monkeypatch, tmp_path):
    fail = {"turn": 2, "flow_ids": ["F1"], "reason": "context overflow: x", "file": "a.js", "line": 3}
    rc, out, _ = _lm_scan(monkeypatch, tmp_path, leads=[_lead(file="a.js")], failures=[fail])
    assert rc == 3
    assert [v["decision"] for v in out] == ["INCONCLUSIVE", "ERROR"]
    assert "could not judge F1" in out[1]["reason"] and out[1]["lead"]["file"] == "a.js"


def test_cli_lm_verify_runs_the_same_model_verifier(monkeypatch, tmp_path):
    calls = {}

    def fake_verify(scan_id, leads, repo, on_event, *, model, stop=None, on_verdict=None, **kw):
        calls["model"], calls["n"] = model, len(leads)
        from orion.contracts import Verdict
        out = [Verdict(lead, "CONFIRM", "re-judged", route="lm-verify") for lead in leads]
        for v in out:
            on_verdict(v)
        return out

    monkeypatch.setattr(lm_verify, "verify_lm", fake_verify)
    rc, out, _ = _lm_scan(monkeypatch, tmp_path, "--lm-verify", "--fail-on", "confirm", leads=[_lead()])
    assert rc == 1 and calls["n"] == 1 and isinstance(calls["model"], _Model)    # --fail-on now trips
    assert out[0]["decision"] == "CONFIRM" and out[0]["route"] == "lm-verify"
