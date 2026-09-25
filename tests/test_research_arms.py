"""PLAN2 arm harnesses: ungrounded-review + semgrep finding mappers, and the claude_cli use_mcp toggle.
Token-free — no subprocess, no API, no semgrep binary needed (mappers take pre-parsed data)."""
from __future__ import annotations

from bench import semgrep_adapter, ungrounded_review
from orion import claude_cli


# --- ungrounded review result -> findings -----------------------------------------------------

def test_ungrounded_findings_from_leads():
    result = {"leads": [
        {"shape": "A", "text": "SQL injection in views.py", "evidence": "objects.raw(q)", "confidence": "HIGH"},
        {"shape": "A", "text": "", "evidence": ""},                 # empty -> dropped
        "not a dict",                                                # junk -> dropped
    ]}
    fs = ungrounded_review.findings_from_result(result)
    assert fs == [("SQL injection in views.py", "objects.raw(q)")]


def test_ungrounded_error_result_yields_no_findings():
    assert ungrounded_review.findings_from_result({"_error": "boom"}) == []
    assert ungrounded_review.findings_from_result({"leads": "nope"}) == []


# --- semgrep json -> findings -----------------------------------------------------------------

def test_semgrep_findings_mapping():
    sg = {"results": [
        {"check_id": "python.django.sql-injection", "path": "app/views.py",
         "start": {"line": 42}, "extra": {"message": "SQL injection via raw()", "lines": "objects.raw(q)"}},
        {"check_id": "x", "path": "a.py", "start": {}, "extra": {}},
    ], "_meta": {"version": "1.0"}}
    fs = semgrep_adapter.findings_from_semgrep(sg)
    assert len(fs) == 2
    text, evidence = fs[0]
    assert "sql-injection" in text and "SQL injection via raw()" in text
    assert "app/views.py:42" in evidence


def test_semgrep_error_yields_no_findings():
    assert semgrep_adapter.findings_from_semgrep({"_error": "not installed"}) == []


# --- claude_cli use_mcp toggle ----------------------------------------------------------------

def _cmd(use_mcp):
    return claude_cli._build_cmd(
        session_id="s", system="sys", json_schema=None, add_dir="/repo",
        extra_allowed=("Read", "Grep", "Glob"), max_turns=10, use_mcp=use_mcp)


def test_use_mcp_true_includes_graph_tools():
    cmd = _cmd(True)
    assert "--mcp-config" in cmd
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "mcp__orion__run_cypher" in allowed


def test_use_mcp_false_drops_graph_tools_and_mcp_config():
    cmd = _cmd(False)
    assert "--mcp-config" not in cmd
    assert "--strict-mcp-config" not in cmd
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "mcp__orion__run_cypher" not in allowed
    assert "Read" in allowed and "Grep" in allowed          # source-reading tools remain


# --- arm O (Orion + Claude) wiring and the prove.py harness -------------------------------------

def test_arm_o_writes_labelled_result_with_every_decision(tmp_path, monkeypatch):
    import json
    from bench import research_eval
    rows = [{"decision": "CONFIRM", "shape": "A", "text": "NoSQL injection via $where"},
            {"decision": "REJECT", "shape": "B", "text": "maybe"}]
    monkeypatch.setattr(research_eval, "_arm_orion", lambda repo, sid, ev: (
        [("NoSQL injection via $where", "q", "app/data/allocations-dao.js"),
         ("stray claim", "", "x.js")], rows))
    out = tmp_path / "O-test.json"
    assert research_eval.main(["--arm", "O", "--benchmark", "nodegoat", "--repo", ".", "--model",
                               "sonnet", "--label", "O-test", "--out", str(out), "--quiet"]) == 0
    r = json.loads(out.read_text())
    assert (r["arm"], r["label"], r["model"]) == ("O", "O-test", "sonnet")
    assert "A1-2" in r["found"]                                   # credited via the structured file
    assert r["decisions"] == {"CONFIRM": 1, "INCONCLUSIVE": 0, "REJECT": 1, "ERROR": 0}
    assert r["false_positive_candidates_detail"] == [{"text": "stray claim", "file": "x.js"}]
    assert r["findings"][0]["file"] == "app/data/allocations-dao.js"


def test_prove_preflight_reports_each_missing_prerequisite(monkeypatch):
    from bench import prove
    monkeypatch.setattr(prove, "_check_claude", lambda: "no claude")
    monkeypatch.setattr(prove, "_check_neo4j", lambda: None)
    monkeypatch.setattr(prove, "_check_joern", lambda: "no joern")
    monkeypatch.setattr(prove, "_check_fixture", lambda b: None if b == "nodegoat" else f"no {b}")
    assert prove.preflight(["nodegoat", "pygoat"]) == ["no claude", "no joern", "no pygoat"]
    assert prove.main(["--dry-run", "--benchmarks", "nodegoat,pygoat"]) == 1
    assert prove.main(["--benchmarks", "juice-shop"]) == 2


def test_prove_table_includes_committed_baselines():
    from bench import prove
    t = prove.table(["nodegoat"], ["O-sonnet"])
    assert "Opus 5, no Orion (C)" in t and "Semgrep (D)" in t
    assert "| 15 / 15 |" in t                                    # the committed Opus-alone row


def test_plot_results_recognises_orion_runs():
    from bench import plot_results as pr
    assert pr._is_arm("O-opus") and pr._is_arm("O") and not pr._is_arm("notes")
    assert pr._label("O-opus", {"model": "opus"}) == "Orion + opus"
    assert pr._arms({"nodegoat": {"O-sonnet": {}, "C": {}, "D": {}}}) == ["C", "D", "O-sonnet"]
