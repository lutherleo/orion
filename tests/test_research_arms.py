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
