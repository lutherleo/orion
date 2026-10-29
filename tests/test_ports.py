"""Merge plan Phase 4, the opt-in ports between the two engines (decision D7). Each is OFF by default
and leaves the default prompts/messages byte-identical. Token-free and Neo4j-free.

  (a) --verify-context: the Claude verifier gets the code around the lead (Orion-LM's code index)
  (b) --shortlists on the lm engine: :CandidateFinding rows become code-only lm cases
  (c) --shortlists repo text sits in CODE markers (tests/test_shortlists.py covers the rendering)
  (d) --runtime / --use-runtime on the lm engine: flows whose sink ran are judged first
"""
from __future__ import annotations

import pytest

from orion import config, verify
from orion.contracts import Lead, Verdict
from orion.lm import engine, tools
from orion.lm import slice as lm_slice
from orion.lm.codeindex import Span

LEAD = Lead(index=0, shape="A", text="sqli in a.py", evidence="q", confidence="HIGH", file="app/a.py",
            line_start=12, cwe="CWE-89")


# --- (a) --verify-context -------------------------------------------------------------------------

def test_verifier_message_is_byte_identical_without_context():
    assert verify._lead_message("s", LEAD) == verify._lead_message("s", LEAD, "", "")


def test_verify_all_builds_no_context_by_default(monkeypatch):
    monkeypatch.setattr(config, "VERIFY_CONTEXT", False)
    seen = {}

    def lead_stub(*a, **kw):
        seen.update(kw)
        return Verdict(lead=a[1], decision="REJECT", reason="")

    monkeypatch.setattr(verify, "verify_lead", lead_stub)
    monkeypatch.setattr(verify, "code_context_builder", lambda *a: pytest.fail("built without --verify-context"))
    verify.verify_all("s", [LEAD], ".", lambda e: None, run_agent=lambda *a, **k: {})
    assert "code_context" not in seen


def test_context_block_and_enclosing_function_reach_the_verifier(monkeypatch):
    messages = []

    def run_agent(sid, system, message, **kw):
        messages.append(message)
        return {"decision": "REJECT", "reason": "r"}

    ctx = lambda lead: ("CODE AROUND THE CLAIMED LOCATION ...\n<<<CODE>>>\ndef view(): pass\n<<<END CODE>>>", "view")
    v = verify.verify_lead("s", LEAD, ".", lambda e: None, run_agent, code_context=ctx)
    assert "CODE AROUND THE CLAIMED LOCATION" in messages[0] and messages[0].endswith("Verify this one lead now.")
    assert v.lead.function == "view"                       # a missing function is filled from the index


def test_missing_tree_sitter_is_a_warning_not_silence(monkeypatch):
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    events = []
    assert verify.code_context_builder(".", events.append) is None
    assert events and events[0]["event"] == "warn" and "tree-sitter" in events[0]["detail"]


# --- (b) shortlist rows as lm code-only cases ----------------------------------------------------

class _Index:
    """A RepoIndex stand-in: one function per file."""
    files = {"app/a.py": [Span("app/a.py", "view", 10, 20, ())], "app/b.py": [Span("app/b.py", "load", 1, 9, ())]}
    skipped: dict = {}

    def enclosing(self, file, line):
        return next((s for s in self.files.get(file, []) if s.start <= line <= s.end), None)


def test_finding_rows_become_code_only_cases_without_repeating_a_flows_target(monkeypatch):
    calls = []
    monkeypatch.setattr(engine.cases, "case_from_function",
                        lambda index, file, span, *, anchor, case_id, budget: calls.append((file, anchor, case_id))
                        or {"flow_id": case_id, "snippets": [{"role": "target", "file": file, "function": span.name}]})
    flows = [{"flow_id": "F0", "snippets": [{"role": "target", "file": "app/a.py", "function": "view"}]}]
    rows = [{"shape": "B", "rank": 0, "file": "app/a.py", "line": 12},       # inside a flow's target
            {"shape": "C", "rank": 0, "file": "app/b.py", "line": 3},
            {"shape": "D", "rank": 0, "file": "app/b.py", "line": 5},        # same function again
            {"shape": "D", "rank": 1, "file": None, "line": None}]           # repo-wide row: no code
    out = engine._finding_cases(rows, _Index(), engine.CaseBudget(), flows, lambda e: None)
    assert [c["flow_id"] for c in out] == ["SC0"] and calls == [("app/b.py", 3, "SC0")]


def test_discover_lm_judges_shortlist_cases_only_when_asked(monkeypatch):
    monkeypatch.setattr(engine, "_load_flows", lambda db, sid, n: [])
    monkeypatch.setattr(engine, "_load_findings", lambda db, sid, n: [{"shape": "C", "rank": 0,
                                                                       "file": "app/b.py", "line": 3}])
    monkeypatch.setattr(engine, "_build_index", lambda repo, on_event: _Index())
    monkeypatch.setattr(engine.cases, "case_from_function",
                        lambda index, file, span, **kw: {"flow_id": kw["case_id"], "snippets": [{"role": "target"}]})
    judged = []
    monkeypatch.setattr(engine, "_session", lambda gen, db, repo, sid, batch, **kw: judged.append(batch) or ([], []))
    engine.discover_lm("s", ".", lambda e: None, model=lambda m: None, db=object(), shortlists=False)
    assert judged == []
    engine.discover_lm("s", ".", lambda e: None, model=lambda m: None, db=object(), shortlists=True)
    assert [b[0]["flow_id"] for b in judged] == ["SC0"]


# --- (d) runtime re-ranking of lm flows ------------------------------------------------------------

POOL = [{"rank": 0, "sink_uid": "k0", "path_uids": ["a", "k0"]},
        {"rank": 1, "sink_uid": "k1", "path_uids": ["b", "c", "k1"]},
        {"rank": 2, "sink_uid": "k2", "path_uids": ["d", "k2"]},
        {"rank": 3, "sink_uid": "k3", "path_uids": ["e", "k3"]}]


def test_runtime_order_puts_executed_sinks_first_then_coverage_then_rank():
    order = [f["rank"] for f in engine._runtime_order(POOL, {"k2", "d", "k1", "k3"})]
    # sink ran for 2, 3 and 1; then by path coverage: 2 (2/2), 3 (1/2), 1 (1/3); 0's sink never ran
    assert order == [2, 3, 1, 0]
    assert [f["rank"] for f in engine._runtime_order(POOL, set())] == [0, 1, 2, 3]


def test_load_flows_without_runtime_data_is_the_plain_top_n(monkeypatch):
    monkeypatch.setattr(lm_slice, "candidate_flows", lambda db, sid, max_flows: POOL[:max_flows])
    monkeypatch.setattr(lm_slice, "executed_uids", lambda db, sid: set())
    monkeypatch.setattr(lm_slice, "flow_slice", lambda db, sid, f: {"flow_id": f"F{f['rank']}"})
    plain = engine._load_flows(None, "s", 2)
    assert plain == engine._load_flows(None, "s", 2, runtime=True) == [{"flow_id": "F0"}, {"flow_id": "F1"}]
    monkeypatch.setattr(lm_slice, "executed_uids", lambda db, sid: {"k3", "e"})
    assert engine._load_flows(None, "s", 2, runtime=True)[0] == {"flow_id": "F3"}


def test_the_new_slice_queries_pass_the_lm_checker():
    class _DB:
        def __init__(self):
            self.q = []

        def run_cypher(self, scan_id, query, limit=50, **kw):
            self.q.append(query)
            return {"row_count": 0, "rows": [], "truncated": False, "query_type": "r"}

    db = _DB()
    lm_slice.candidate_findings(db, "s")
    lm_slice.executed_uids(db, "s")
    real = [q for q in db.q if not q.startswith("EXPLAIN")]
    assert len(real) == 2 and all(tools.check_query(q) is None for q in real)
